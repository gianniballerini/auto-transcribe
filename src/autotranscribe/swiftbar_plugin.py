#!/usr/bin/env python3
# <swiftbar.title>Autotranscribe</swiftbar.title>
# <swiftbar.desc>Shows what autotranscribe.py is transcribing.</swiftbar.desc>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
"""SwiftBar plugin: menu bar status for autotranscribe (refreshes every 2 s).

`autotranscribe setup` installs a copy whose shebang points at the installed interpreter.
"""

import json
import os
import time
from pathlib import Path

STATUS_PATH = Path.home() / "Library/Caches/autotranscribe/status.json"
HIDDEN_FLAG = STATUS_PATH.parent / "hidden"  # empty output hides the item in SwiftBar
MAX_NAME_CHARS = 20


def read_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def pid_alive(pid) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(max(seconds, 0)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def short(name: str) -> str:
    stem = Path(name).stem
    return stem if len(stem) <= MAX_NAME_CHARS else stem[: MAX_NAME_CHARS - 1] + "…"


def open_item(label: str, path: Path, extra: str = "") -> str:
    return f"{label} | bash=/usr/bin/open param1={path} terminal=false {extra}".rstrip()


def main() -> None:
    status = read_status()
    watch_dir = Path(status.get("watch_dir") or Path.home() / "Transcriptions")
    working = status.get("state") == "working"
    interrupted = working and not pid_alive(status.get("pid"))
    if interrupted:
        working = False
    if not working and HIDDEN_FLAG.exists():
        return

    if working:
        elapsed = fmt_duration(time.time() - status.get("started_at", time.time()))
        print(f"{short(status.get('file', '?'))} · {elapsed} | sfimage=waveform.circle.fill")
        print("---")
        print(f"Transcribing {status.get('file', '?')}")
        print(f"Stage: {status.get('stage', '?')} · {elapsed} elapsed")
        queue = status.get("queue", 0)
        print(f"Queue: {queue} more" if queue else "Queue: empty")
    else:
        print("| sfimage=waveform")
        print("---")
        print(f"Idle — drop a file into {watch_dir}")
        if interrupted:
            print(f"⚠ Last run interrupted ({status.get('file', '?')}) | color=orange")

    last = status.get("last")
    if last:
        mark = "✓" if last.get("ok") else "✗"
        detail = f"{last.get('phrases', 0)} phrases, " if last.get("ok") else "failed, "
        print("---")
        print(f"Last: {last.get('file', '?')} {mark} ({detail}{last.get('seconds', 0)}s)")

    print("---")
    print(open_item("Open output folder", watch_dir / "output"))
    print(open_item("Open drop folder", watch_dir))
    print(f"Open log | bash=/usr/bin/open param1=-a param2=Console param3={watch_dir / '.watcher.log'} terminal=false")
    failed = watch_dir / "failed"
    if failed.is_dir() and any(failed.iterdir()):
        print(open_item("Open failed/", failed, "color=red"))
    print("---")
    print(f"Hide until next transcription | bash=/usr/bin/touch param1={HIDDEN_FLAG} "
          "terminal=false refresh=true")


if __name__ == "__main__":
    main()
