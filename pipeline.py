"""Turn the Instagram posts saved in a DM export into Obsidian notes.

Stages. Each stage picks up the posts the previous one finished, so any stage can be
re-run, interrupted, or limited to a few posts:

  ingest      export JSON -> data/manifest.sqlite (one row per post, deduped by shortcode)
  download    gallery-dl, yt-dlp as fallback -> data/media/<id>/
  transcribe  whisper for videos -> data/transcripts/<id>.txt
              video frames + resized images -> data/visuals/<id>/
  queue       one packet per post for Claude -> data/packets/<id>.md
  (Claude)    a Claude Code session reads each packet and its images and writes
              data/claude/<id>.json (see .claude/skills/enrich-notes/)
  collect     validates data/claude/<id>.json; posts with nothing worth keeping
              are marked "skipped"
  render      data/claude/<id>.json -> notes/<Title>.md (only notes not written yet)
  merge       fold duplicate notes into one: the kept note lists every source

Usage:
  .venv/bin/python pipeline.py ingest [export.json ...]
  .venv/bin/python pipeline.py download [--limit N]
  .venv/bin/python pipeline.py transcribe [--limit N]
  .venv/bin/python pipeline.py queue [--limit N]      # prints the post ids waiting for Claude
  .venv/bin/python pipeline.py collect
  .venv/bin/python pipeline.py render [--force]
  .venv/bin/python pipeline.py all [--limit N]         # everything except the Claude step
  .venv/bin/python pipeline.py status [-v]
  .venv/bin/python pipeline.py retry          # give failed posts another 3 attempts
  .venv/bin/python pipeline.py merge KEEP DUP [DUP ...]   # after writing the merged
                                               # data/claude/KEEP.json

Instagram cookies come from data/cookies.txt when it exists, otherwise straight from Brave.
"""

import argparse
import json
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = DATA / "manifest.sqlite"
MEDIA = DATA / "media"
TRANSCRIPTS = DATA / "transcripts"
VISUALS = DATA / "visuals"
CLAUDE = DATA / "claude"
PACKETS = DATA / "packets"
NOTES = ROOT / "notes"
COOKIES = DATA / "cookies.txt"
DEFAULT_EXPORT = ROOT / "export-dump" / "messages-to-work-on.json"

BROWSER = "brave"
GALLERY_DL = str(Path(sys.executable).parent / "gallery-dl")
YT_DLP = shutil.which("yt-dlp") or "yt-dlp"
SLEEP = (5, 15)  # seconds between posts, like the old yt-dlp command
MAX_ATTEMPTS = 3
MAX_CONSECUTIVE_FAILURES = 5  # Instagram is probably blocking us; stop instead of burning attempts

WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
FRAME_EDGE = 1024  # long edge of frames taken from videos
IMAGE_EDGE = 1568  # long edge of post images; Claude downscales anything bigger anyway
MAX_VISUALS = 40  # per post; multi-video carousels reach ~33 frames

STAGES = ["ingested", "downloaded", "transcribed", "enriched", "skipped", "merged"]
LINK = re.compile(r"instagram\.com/(?:[\w.]+/)?(p|reels?|tv)/([\w-]+)")

TYPES = [
    "recipe", "food-and-places", "travel", "books", "film-and-tv", "music",
    "philosophy", "psychology", "self-improvement", "productivity", "career", "money",
    "tech", "science", "history", "health", "fitness", "relationships",
    "design-and-art", "language-and-writing", "shopping", "home-and-diy", "other",
]  # fmt: skip

# keep in sync with .claude/skills/enrich-notes/note-guide.md
NOTE_FIELDS = {"useful": bool, "reason": str, "title": str, "type": str, "tags": list, "links": list, "content": str}


# --- manifest -----------------------------------------------------------------------


def connect() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,         -- Instagram shortcode
            url TEXT NOT NULL,
            saved_at TEXT NOT NULL,      -- when it was sent to the chat
            share_text TEXT,             -- caption snippet from the export
            caption TEXT,                -- full caption, from the download
            stage TEXT NOT NULL DEFAULT 'ingested',
            attempts INTEGER NOT NULL DEFAULT 0,  -- failed attempts at the next stage
            error TEXT,                  -- last failure
            reason TEXT,                 -- why Claude skipped it
            note TEXT,                   -- file name in notes/
            merged_into TEXT             -- id of the note this duplicate was folded into
        )"""
    )
    if "merged_into" not in {r["name"] for r in db.execute("PRAGMA table_info(posts)")}:
        db.execute("ALTER TABLE posts ADD COLUMN merged_into TEXT")
    return db


def pending(db, stage: str, limit: int | None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM posts WHERE stage = ? AND attempts < ? ORDER BY saved_at DESC"
    rows = db.execute(sql, (stage, MAX_ATTEMPTS)).fetchall()
    return rows[:limit] if limit else rows


def advance(db, pid: str, stage: str, **fields) -> None:
    fields = {"stage": stage, "attempts": 0, "error": None, **fields}
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE posts SET {cols} WHERE id = ?", (*fields.values(), pid))
    db.commit()


def fail(db, pid: str, error: str) -> None:
    db.execute(
        "UPDATE posts SET attempts = attempts + 1, error = ? WHERE id = ?",
        (error.strip()[-500:], pid),
    )
    db.commit()


# --- ingest -------------------------------------------------------------------------


def fix_mojibake(s: str) -> str:
    """Instagram exports write UTF-8 bytes as if they were Latin-1."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def ingest(db, paths: list[Path]) -> None:
    known = {r["id"] for r in db.execute("SELECT id FROM posts")}
    added = links = other = 0
    for path in paths:
        for msg in json.loads(path.read_text())["messages"]:
            share = msg.get("share") or {}
            m = LINK.search(share.get("link", ""))
            if not m:
                other += 1
                continue
            links += 1
            kind = "p" if m.group(1) == "p" else "reel"
            pid = m.group(2)
            saved = datetime.fromtimestamp(msg["timestamp_ms"] / 1000).isoformat(timespec="seconds")
            db.execute(
                """INSERT INTO posts (id, url, saved_at, share_text) VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET saved_at = min(saved_at, excluded.saved_at)""",
                (pid, f"https://www.instagram.com/{kind}/{pid}/", saved,
                 fix_mojibake(share.get("share_text") or "")),
            )  # fmt: skip
            if pid not in known:
                known.add(pid)
                added += 1
    db.commit()
    print(f"Ingest: {links} post links, {added} new posts, {other} messages without a post link.")


# --- download -----------------------------------------------------------------------


def media_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    exts = VIDEO_EXTS | IMAGE_EXTS
    return sorted(f for f in folder.iterdir() if f.suffix.lower() in exts)


def cookie_args() -> list[str]:
    return ["--cookies", str(COOKIES)] if COOKIES.exists() else ["--cookies-from-browser", BROWSER]


def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout + r.stderr


def read_caption(folder: Path) -> str:
    for f in sorted(folder.glob("*info.json")):
        info = json.loads(f.read_text())
        if text := info.get("description") or info.get("caption"):
            return text
    return ""


def download(db, limit: int | None) -> None:
    """Stops early (without failing `all`) on a login wall or what looks like rate limiting."""
    # posts that failed before go last: a few dead posts shouldn't look like rate limiting
    todo = sorted(pending(db, "ingested", None), key=lambda p: p["attempts"])[:limit]
    if not todo:
        print("Download: nothing to do.")
        return
    streak = 0
    for i, post in enumerate(todo, 1):
        if i > 1:
            time.sleep(random.uniform(*SLEEP))
        pid, dest = post["id"], MEDIA / post["id"]
        print(f"[{i}/{len(todo)}] {post['url']}", flush=True)
        dest.mkdir(parents=True, exist_ok=True)
        log = run([GALLERY_DL, *cookie_args(), "-D", str(dest), "-f", "{num:>02}.{extension}",
                   "--write-info-json", post["url"]])  # fmt: skip
        if "accounts/login" in log or "cookies database" in log:
            print(f"Instagram wants a login. Refresh data/cookies.txt (see README).\n{log}")
            return
        if not media_files(dest):
            log += run([YT_DLP, *cookie_args(), "--write-info-json", "-o",
                        str(dest / "01.%(ext)s"), post["url"]])  # fmt: skip
        if files := media_files(dest):
            streak = 0
            advance(db, pid, "downloaded", caption=read_caption(dest))
            print(f"  -> {len(files)} file(s)")
            continue
        streak += post["attempts"] == 0
        fail(db, pid, log)
        print(f"  failed: {log.strip().splitlines()[-1] if log.strip() else 'no output'}")
        if streak >= MAX_CONSECUTIVE_FAILURES:
            print(f"{streak} new downloads failed in a row. Instagram may be rate limiting; stopping downloads for now.")
            return


# --- transcribe ---------------------------------------------------------------------


def has_audio(video: Path) -> bool:
    out = run(["ffprobe", "-v", "error", "-select_streams", "a",
               "-show_entries", "stream=index", "-of", "csv=p=0", str(video)])  # fmt: skip
    return bool(out.strip())


def duration(video: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(video)])  # fmt: skip
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def to_jpeg(src: Path, dest: Path, edge: int, at: float | None = None) -> None:
    """Write src (an image, or the frame of a video at `at` seconds) as a JPEG of at most `edge` px."""
    seek = ["-ss", f"{at:.2f}"] if at is not None else []
    scale = f"scale='min(iw,{edge}*iw/max(iw,ih))':-2"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *seek, "-i", str(src), "-frames:v", "1",
         "-vf", scale, "-q:v", "4", str(dest)],
        check=True,
    )  # fmt: skip


def frame_times(video: Path) -> list[float]:
    """3-8 evenly spaced timestamps: about one frame per 8 seconds."""
    d = duration(video)
    n = max(3, min(8, round(d / 8)))
    return [(k + 0.5) * d / n for k in range(n)] if d else [0.0]


def transcribe(db, limit: int | None) -> None:
    todo = pending(db, "downloaded", limit)
    if not todo:
        print("Transcribe: nothing to do.")
        return
    import mlx_whisper  # slow to import; only needed here

    for i, post in enumerate(todo, 1):
        pid = post["id"]
        files = media_files(MEDIA / pid)
        print(f"[{i}/{len(todo)}] {pid}: {len(files)} file(s)", flush=True)
        visuals = VISUALS / pid
        shutil.rmtree(visuals, ignore_errors=True)
        visuals.mkdir(parents=True)
        parts = []
        try:
            for f in files:
                if f.suffix.lower() in IMAGE_EXTS:
                    to_jpeg(f, visuals / f"{f.stem}.jpg", IMAGE_EDGE)
                    continue
                for k, t in enumerate(frame_times(f)):
                    to_jpeg(f, visuals / f"{f.stem}_frame{k:02}.jpg", FRAME_EDGE, at=t)
                if has_audio(f):
                    # condition_on_previous_text=False: otherwise turbo sometimes skips whole
                    # stretches of speech in Hindi/Hinglish reels
                    result = mlx_whisper.transcribe(
                        str(f), path_or_hf_repo=WHISPER_MODEL, condition_on_previous_text=False
                    )
                    text = "\n".join(seg["text"].strip() for seg in result["segments"])
                    parts.append(f"[{f.name}, language: {result['language']}]\n{text}")
        except Exception as e:
            fail(db, pid, f"{type(e).__name__}: {e}")
            print(f"  failed: {e}", file=sys.stderr)
            continue
        TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
        (TRANSCRIPTS / f"{pid}.txt").write_text("\n\n".join(parts) + "\n" if parts else "")
        advance(db, pid, "transcribed")
        print(f"  -> {len(list(visuals.iterdir()))} image(s), {len(parts)} transcript(s)")


# --- queue / collect ----------------------------------------------------------------


def write_packet(post) -> Path:
    """Everything Claude needs for one post, in one file, with absolute image paths."""
    pid = post["id"]
    images = sorted((VISUALS / pid).glob("*.jpg"))[:MAX_VISUALS]
    frames = sum("_frame" in f.name for f in images)
    transcript = (TRANSCRIPTS / f"{pid}.txt").read_text().strip()
    lines = [
        f"# {pid}",
        "",
        f"Post: {post['url']}",
        f"Result file: {CLAUDE / f'{pid}.json'}",
        "",
        "## Caption",
        "",
        post["caption"] or post["share_text"] or "(none)",
        "",
        "## Transcript",
        "",
        transcript or "(no speech)",
        "",
        f"## Images ({frames} video frame(s), {len(images) - frames} post image(s))",
        "",
        *(f"- {f}" for f in images),
    ]
    PACKETS.mkdir(parents=True, exist_ok=True)
    path = PACKETS / f"{pid}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def queue(db, limit: int | None) -> None:
    collect(db, quiet=True)
    todo = pending(db, "transcribed", limit)
    for post in todo:
        write_packet(post)
    print(f"{len(todo)} post(s) waiting for Claude. Packets in data/packets/:")
    for post in todo:
        print(post["id"])


def validate(data) -> str | None:
    if not isinstance(data, dict) or set(data) != set(NOTE_FIELDS):
        return f"fields must be exactly {sorted(NOTE_FIELDS)}"
    for key, kind in NOTE_FIELDS.items():
        if not isinstance(data[key], kind):
            return f"{key} must be {kind.__name__}"
    if not all(isinstance(x, str) for x in data["tags"] + data["links"]):
        return "tags and links must be lists of strings"
    if data["type"] not in TYPES:
        return f"type {data['type']!r} is not one of the allowed types"
    if data["useful"] and not (data["title"].strip() and data["content"].strip()):
        return "useful notes need a title and content"
    if not data["useful"] and not data["reason"].strip():
        return "skipped posts need a reason"
    return None


def collect(db, quiet: bool = False) -> None:
    counts = {"enriched": 0, "skipped": 0, "invalid": 0}
    for post in pending(db, "transcribed", None):
        path = CLAUDE / f"{post['id']}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            problem = validate(data)
        except json.JSONDecodeError as e:
            problem = f"invalid JSON: {e}"
        if problem:
            counts["invalid"] += 1
            fail(db, post["id"], f"{path.name}: {problem}")
            print(f"  {post['id']}: {problem}")
        elif data["useful"]:
            counts["enriched"] += 1
            advance(db, post["id"], "enriched", reason=None)
        else:
            counts["skipped"] += 1
            advance(db, post["id"], "skipped", reason=data["reason"])
    if not quiet or any(counts.values()):
        print(f"Collect: {counts['enriched']} note(s), {counts['skipped']} skipped, {counts['invalid']} invalid.")


# --- render -------------------------------------------------------------------------


def clean_name(s: str) -> str:
    """Safe as both a file name and an Obsidian [[link]]."""
    s = re.sub(r'[\\/*?"<>|#^\[\]]', " ", s.replace(":", " -"))
    return re.sub(r"\s+", " ", s).strip(" .")[:100]


def note_markdown(post, data: dict, also: list[str] = ()) -> str:
    """`also`: source URLs of duplicate posts merged into this one."""
    tags = [re.sub(r"[^a-z0-9-]+", "-", t.lower()).strip("-") for t in data["tags"]]
    sources = [post["url"], *also]
    lines = [
        "---",
        f"source: {post['url']}" if not also else "source:\n" + "\n".join(f"  - {u}" for u in sources),
        f"saved: {post['saved_at'][:10]}",
        f"type: {data['type']}",
        f"tags: [{', '.join(t for t in tags if t)}]",
        "---",
        "",
        f"# {data['title']}",
        "",
        data["content"].strip(),
    ]
    links = [name for name in map(clean_name, data["links"]) if name]
    if links:
        lines += ["", "Related: " + " · ".join(f"[[{name}]]" for name in links)]
    return "\n".join(lines) + "\n"


def render(db, force: bool) -> None:
    if force:
        for (name,) in db.execute("SELECT note FROM posts WHERE note IS NOT NULL").fetchall():
            (NOTES / name).unlink(missing_ok=True)
        db.execute("UPDATE posts SET note = NULL")
    NOTES.mkdir(exist_ok=True)
    taken = {r["note"].lower() for r in db.execute("SELECT note FROM posts WHERE note IS NOT NULL")}
    todo = db.execute("SELECT * FROM posts WHERE stage = 'enriched' AND note IS NULL ORDER BY saved_at").fetchall()
    for post in todo:
        data = json.loads((CLAUDE / f"{post['id']}.json").read_text())
        base = clean_name(data["title"]) or post["id"]
        name, n = f"{base}.md", 2
        while name.lower() in taken or (NOTES / name).exists():
            name, n = f"{base} ({n}).md", n + 1
        taken.add(name.lower())
        also = [r["url"] for r in db.execute(
            "SELECT url FROM posts WHERE merged_into = ? ORDER BY saved_at", (post["id"],))]
        (NOTES / name).write_text(note_markdown(post, data, also))
        db.execute("UPDATE posts SET note = ? WHERE id = ?", (name, post["id"]))
    db.commit()
    print(f"Render: {len(todo)} new note(s) in notes/.")


def merge(db, keep: str, dups: list[str]) -> None:
    """Fold duplicate notes into `keep`, whose data/claude JSON already holds the merged note."""
    ids = [keep, *dups]
    rows = {r["id"]: r for r in db.execute(
        f"SELECT * FROM posts WHERE id IN ({','.join('?' * len(ids))})", ids)}
    for pid in ids:
        if pid not in rows or rows[pid]["stage"] != "enriched":
            sys.exit(f"{pid}: not an enriched post")
    if problem := validate(json.loads((CLAUDE / f"{keep}.json").read_text())):
        sys.exit(f"{keep}.json: {problem}")
    for pid in ids:
        if rows[pid]["note"]:
            (NOTES / rows[pid]["note"]).unlink(missing_ok=True)
    db.executemany("UPDATE posts SET stage = 'merged', merged_into = ?, note = NULL WHERE id = ?",
                   [(keep, d) for d in dups])  # fmt: skip
    db.execute("UPDATE posts SET note = NULL WHERE id = ?", (keep,))
    db.commit()
    print(f"Merged {', '.join(dups)} into {keep}.")
    render(db, force=False)


# --- status -------------------------------------------------------------------------


def status(db, verbose: bool) -> None:
    total = db.execute("SELECT count(*) FROM posts").fetchone()[0]
    print(f"{total} posts")
    for stage in STAGES:
        n = db.execute("SELECT count(*) FROM posts WHERE stage = ?", (stage,)).fetchone()[0]
        print(f"  {stage:<12} {n}")
    notes = db.execute("SELECT count(*) FROM posts WHERE note IS NOT NULL").fetchone()[0]
    print(f"  notes        {notes}")
    failed = db.execute(
        "SELECT * FROM posts WHERE error IS NOT NULL ORDER BY stage, saved_at DESC"
    ).fetchall()
    if failed:
        gave_up = sum(p["attempts"] >= MAX_ATTEMPTS for p in failed)
        print(f"\n{len(failed)} failed at the step after their stage ({gave_up} out of attempts; `retry` resets them)")
        for p in failed if verbose else failed[:10]:
            last = p["error"].strip().splitlines()[-1] if p["error"].strip() else ""
            print(f"  {p['id']:<14} {p['stage']:<11} x{p['attempts']}  {last[:100]}")
    if verbose:
        for p in db.execute("SELECT id, reason FROM posts WHERE stage = 'skipped'"):
            print(f"  skipped {p['id']:<14} {p['reason']}")


# --- cli ----------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("exports", nargs="*", type=Path, default=[DEFAULT_EXPORT])
    for name in ("download", "transcribe", "queue", "all"):
        sub.add_parser(name).add_argument("--limit", type=int, help="process at most N posts, newest first")
    sub.add_parser("collect")
    sub.add_parser("render").add_argument("--force", action="store_true", help="rewrite every note")
    sub.add_parser("status").add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("retry")
    p = sub.add_parser("merge")
    p.add_argument("keep")
    p.add_argument("dups", nargs="+")
    args = ap.parse_args()

    db = connect()
    match args.cmd:
        case "ingest":
            ingest(db, args.exports)
        case "download":
            download(db, args.limit)
        case "transcribe":
            transcribe(db, args.limit)
        case "queue":
            queue(db, args.limit)
        case "collect":
            collect(db)
        case "render":
            render(db, args.force)
        case "all":
            ingest(db, [DEFAULT_EXPORT])
            download(db, args.limit)
            transcribe(db, args.limit)
            collect(db)
            render(db, force=False)
            queue(db, args.limit)
        case "merge":
            merge(db, args.keep, args.dups)
        case "status":
            status(db, args.verbose)
        case "retry":
            n = db.execute("UPDATE posts SET attempts = 0, error = NULL WHERE error IS NOT NULL").rowcount
            db.commit()
            print(f"Reset {n} failed post(s).")


if __name__ == "__main__":
    main()
