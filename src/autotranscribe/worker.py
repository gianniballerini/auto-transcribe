"""
Transcribe every media file dropped into a folder.

Each pending file in the watch folder is converted to 16kHz mono WAV (ffmpeg),
transcribed with MLX Whisper (word timestamps), split into phrases, and written
to output/<stem>.txt (one phrase per line) and output/<stem>.srt.
The original is then moved to processed/ (or failed/ on error).

Meant to be triggered by a launchd WatchPaths agent, but safe to run by hand:
  autotranscribe run
  autotranscribe run --language es
"""

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_WATCH_DIR = Path.home() / "Transcriptions"
# Outside the watch folder on purpose: writes there would re-trigger the launchd WatchPaths job.
STATUS_PATH = Path.home() / "Library/Caches/autotranscribe/status.json"
# Created by the menu bar plugin's "Hide" item; removed here so the icon reappears on new work.
HIDDEN_FLAG = STATUS_PATH.parent / "hidden"

IGNORED_SUFFIXES = {".part", ".crdownload", ".download", ".tmp", ".py", ".log", ".plist", ".md"}
STABLE_SECONDS = 5

SENTENCE_END = (".", "?", "!", "…")
SOFT_BREAK = (",", ";", ":")
MAX_PHRASE_SECONDS = 7.0
MAX_PHRASE_CHARS = 84
PAUSE_BREAK_SECONDS = 1.0


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def write_status(**fields) -> None:
    """Merge fields into the status file read by the menu bar plugin. Never raises."""
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            status = json.loads(STATUS_PATH.read_text())
        except (OSError, ValueError):
            status = {}
        status.update(fields, pid=os.getpid(), updated_at=time.time())
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(status))
        os.replace(tmp, STATUS_PATH)
    except Exception as e:
        log(f"Could not write status: {e}")


def extract_wav(input_path: Path, out_path: Path) -> None:
    """Extract/convert input media to 16kHz mono WAV using ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-ar", "16000",
        "-ac", "1",
        "-vn",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr}")


def transcribe(wav_path: Path, model: str, language: str | None) -> dict:
    """Run MLX Whisper transcription with word-level timestamps."""
    import mlx_whisper  # imported lazily: slow, and not needed when nothing is pending

    kwargs = {"path_or_hf_repo": model, "word_timestamps": True, "verbose": False}
    if language:
        kwargs["language"] = language
    return mlx_whisper.transcribe(str(wav_path), **kwargs)


def to_phrases(transcript: dict) -> list[dict]:
    """Group Whisper words into phrases, cut at sentence ends or length caps."""
    words = [w for seg in transcript["segments"] for w in seg.get("words", [])]
    if not words:
        # Fallback: no word timestamps available, use raw segments.
        return [
            {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
            for s in transcript["segments"] if s["text"].strip()
        ]

    phrases: list[dict] = []
    current: list[dict] = []

    def flush(upto: int) -> None:
        chunk, rest = current[:upto], current[upto:]
        text = "".join(w["word"] for w in chunk).strip()
        if text:
            phrases.append({"start": chunk[0]["start"], "end": chunk[-1]["end"], "text": text})
        current[:] = rest

    for word in words:
        if current and word["start"] - current[-1]["end"] > PAUSE_BREAK_SECONDS:
            flush(len(current))
        current.append(word)
        token = word["word"].strip()
        if token.endswith(SENTENCE_END):
            flush(len(current))
            continue

        duration = current[-1]["end"] - current[0]["start"]
        length = len("".join(w["word"] for w in current).strip())
        if duration > MAX_PHRASE_SECONDS or length > MAX_PHRASE_CHARS:
            # Prefer breaking after the last comma-like token; else before the current word.
            cut = next(
                (i + 1 for i in range(len(current) - 2, -1, -1)
                 if current[i]["word"].strip().endswith(SOFT_BREAK)),
                max(len(current) - 1, 1),
            )
            flush(cut)

    if current:
        flush(len(current))
    return phrases


def fmt_timestamp_srt(seconds: float, sep: str = ",") -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def write_txt(phrases: list[dict], out_path: Path) -> None:
    with open(out_path, "w") as f:
        for p in phrases:
            f.write(f"{p['text']}\n")


def write_srt(phrases: list[dict], out_path: Path) -> None:
    with open(out_path, "w") as f:
        for i, p in enumerate(phrases, start=1):
            f.write(f"{i}\n")
            f.write(f"{fmt_timestamp_srt(p['start'])} --> {fmt_timestamp_srt(p['end'])}\n")
            f.write(f"{p['text']}\n\n")


def unique_stem(directory: Path, stem: str, suffixes: list[str]) -> str:
    """Return a stem that collides with no existing file for any of the suffixes."""
    candidate, n = stem, 1
    while any((directory / f"{candidate}{s}").exists() for s in suffixes):
        n += 1
        candidate = f"{stem}_{n}"
    return candidate


def move_unique(src: Path, dest_dir: Path) -> Path:
    stem = unique_stem(dest_dir, src.stem, [src.suffix])
    dest = dest_dir / f"{stem}{src.suffix}"
    shutil.move(str(src), dest)
    return dest


def candidates(watch_dir: Path) -> dict[Path, int]:
    """Media candidates in the folder root, mapped to their current size."""
    found = {}
    for path in sorted(watch_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in IGNORED_SUFFIXES:
            continue
        found[path] = path.stat().st_size
    return found


def pending_files(watch_dir: Path) -> tuple[list[Path], bool]:
    """Return (ready files, whether some files are still being written).

    A file is ready when its size is unchanged across STABLE_SECONDS. Size is used
    instead of mtime because Finder copies preserve the original modification date.
    """
    before = candidates(watch_dir)
    if not before:
        return [], False
    time.sleep(STABLE_SECONDS)
    after = candidates(watch_dir)
    ready = [p for p, size in after.items() if before.get(p) == size]
    return ready, len(ready) < len(after)


def process(path: Path, dirs: dict[str, Path], model: str, language: str | None) -> None:
    log(f"Processing {path.name}")
    started = time.time()
    HIDDEN_FLAG.unlink(missing_ok=True)
    write_status(state="working", file=path.name, stage="converting", started_at=started)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "audio.wav"
            extract_wav(path, wav_path)
            write_status(stage="transcribing")
            transcript = transcribe(wav_path, model, language)

        write_status(stage="writing")
        phrases = to_phrases(transcript)
        stem = unique_stem(dirs["output"], path.stem, [".txt", ".srt"])
        write_txt(phrases, dirs["output"] / f"{stem}.txt")
        write_srt(phrases, dirs["output"] / f"{stem}.srt")
        move_unique(path, dirs["processed"])
        log(f"Done {path.name} -> output/{stem}.txt|.srt "
            f"({len(phrases)} phrases, {time.time() - started:.0f}s)")
        write_status(last={"file": path.name, "ok": True, "phrases": len(phrases),
                           "seconds": round(time.time() - started), "finished_at": time.time()})
    except Exception:
        error = traceback.format_exc()
        log(f"FAILED {path.name}\n{error}")
        moved = move_unique(path, dirs["failed"])
        (dirs["failed"] / f"{moved.stem}.error.log").write_text(error)
        write_status(last={"file": path.name, "ok": False, "phrases": 0,
                           "seconds": round(time.time() - started), "finished_at": time.time()})


def run(watch_dir: Path, model: str = DEFAULT_MODEL, language: str | None = None) -> None:
    """Process every pending file in watch_dir until the folder is empty."""
    watch_dir = watch_dir.expanduser().resolve()
    dirs = {name: watch_dir / name for name in ("output", "processed", "failed")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    lock_file = open(watch_dir / ".autotranscribe.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # Another run is active; it re-scans until the folder is empty.

    write_status(watch_dir=str(watch_dir))
    try:
        while True:
            ready, unstable = pending_files(watch_dir)
            for i, path in enumerate(ready):
                write_status(queue=len(ready) - i - 1)
                process(path, dirs, model, language)
            if not ready and not unstable:
                break
    finally:
        write_status(state="idle", file=None, stage=None, queue=0)
