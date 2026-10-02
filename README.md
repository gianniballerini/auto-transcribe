# auto-transcribe

**Drop a file in a folder. Get a transcript. Nothing leaves your Mac.**

![macOS](https://img.shields.io/badge/macOS-Apple%20Silicon-000?logo=apple)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Whisper](https://img.shields.io/badge/Whisper-large--v3--turbo-412991)
![Offline](https://img.shields.io/badge/runs-100%25%20offline-2ea44f)

Voice notes, meetings, interviews, screen recordings: drag them into `~/Transcriptions` and a background job turns them into a clean transcript and `.srt` subtitles using Whisper running locally on Apple Silicon (MLX). No app to open, no upload, no API key, no subscription.

<!-- TODO: add demo GIF here: drag a file into the folder, menu bar shows progress, open the .txt -->

```
~/Transcriptions/
├── meeting.m4a            ← you drop this
├── output/
│   ├── meeting.txt        ← one phrase per line
│   └── meeting.srt        ← ready for any video player or editor
└── processed/meeting.m4a  ← original, moved out of the way
```

## Why

- **Private**: audio is processed on your machine. Nothing is uploaded.
- **Fast**: a 32 s voice note is done in ~10 s on an M2 Pro, model load included.
- **Zero friction**: no window, no button. Drop a file and walk away. It also picks up files dropped while your Mac was off.
- **Any format**: if ffmpeg can read it (`.mp3 .m4a .ogg .wav .mp4 .mov` and more), it gets transcribed.
- **Readable output**: phrases are split at sentence ends and pauses, not at arbitrary 30 s chunks.
- **~100 languages**, auto-detected.
- **Menu bar status** (optional): shows the current file, elapsed time and last result.

## Quick start

Needs an Apple Silicon Mac (M1 or newer), [Homebrew](https://brew.sh), and ~2 GB of disk for the model.

```sh
brew install ffmpeg uv
uv tool install git+https://github.com/gianniballerini/auto-transcribe.git
autotranscribe setup
```

That's it. Drop a file into `~/Transcriptions`. The first run downloads the Whisper model (~1.5 GB), so it takes a few minutes longer.

`setup` creates `~/Transcriptions`, installs a background job that starts at login, and installs the menu bar plugin.

| Result | Location |
| --- | --- |
| Transcript, one phrase per line | `~/Transcriptions/output/<name>.txt` |
| Subtitles | `~/Transcriptions/output/<name>.srt` |
| Original file, after success | `~/Transcriptions/processed/` |
| Original file + error log, after failure | `~/Transcriptions/failed/` |

No speaker recognition (yet).

Optional menu bar status (file name, elapsed time, last result):

```sh
brew install --cask swiftbar
```

If SwiftBar was not configured before `setup`, `setup` prints the plugin folder to select in SwiftBar.

## Update and uninstall

```sh
uv tool upgrade auto-transcribe
autotranscribe setup        # re-run after upgrading

autotranscribe uninstall    # removes the background job and menu bar plugin, keeps your files
uv tool uninstall auto-transcribe
```

## Options

`setup` and `run` accept:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--language` | auto-detect | Force a language code (`es`, `en`, …) |
| `--model` | `mlx-community/whisper-large-v3-turbo` | Any MLX Whisper model on Hugging Face |
| `--watch-dir` | `~/Transcriptions` | Folder to watch |

Passing a flag to `setup` makes the background job always use it. `--no-swiftbar` skips the menu bar plugin.

## How it works

```
file dropped ──► launchd notices folder change ──► autotranscribe run
                                                     │
            ┌────────────────────────────────────────┘
            ▼
   wait until file size is stable (still copying?)
            ▼
   ffmpeg → 16 kHz mono WAV  (any format ffmpeg can read)
            ▼
   MLX Whisper (whisper-large-v3-turbo) with word timestamps
            ▼
   split words into phrases → write .txt + .srt → move original
```

- **Worker** (`worker.py`): every run scans the folder, processes every pending file, then exits. Running it twice is harmless.
  - **Lock** (`.autotranscribe.lock`): if a run is already active, a new trigger exits right away. The active run keeps scanning until the folder is empty.
  - **Stability check**: a file is processed only when its size hasn't changed for 5 s. This uses size, not modification date, because Finder copies keep the original date.
  - **Ignored files**: hidden files, folders, and `.py .md .log .plist .part .crdownload .download .tmp`. Every other file goes to ffmpeg. Anything ffmpeg can't read goes to `failed/`.
  - **Phrase splitting** (`to_phrases`): a new phrase starts at a sentence end (`. ? ! …`), at a pause longer than 1 s, or past 7 s / 84 characters. When it has to cut a long phrase, it prefers to cut after a comma.
  - **Name collisions**: if `output/foo.txt` already exists, the new file becomes `foo_2.txt`.
- **Background job** (`~/Library/LaunchAgents/com.gianniballerini.autotranscribe.plist`, written by `setup`):
  - `WatchPaths` runs the worker whenever the folder changes; `RunAtLoad` also runs it at login.
  - It calls the Python inside the `uv` tool environment, so no system Python setup is needed.
- **Menu bar plugin** (`swiftbar_plugin.py`): reads `~/Library/Caches/autotranscribe/status.json`, which the worker writes. The status file lives outside the watched folder because writing inside it would re-trigger the job. **Hide until next transcription** hides the icon until the next file starts.

### Why `~/Transcriptions` and not `~/Documents`

macOS privacy protection (TCC) covers `~/Desktop`, `~/Documents`, `~/Downloads`, iCloud Drive and external drives. A background job working there gets "Operation not permitted" unless Python has Full Disk Access. A plain folder in your home directory needs no extra permissions.

## Troubleshooting

```sh
launchctl print gui/$(id -u)/com.gianniballerini.autotranscribe | rg 'state|runs|last exit'
tail -f ~/Transcriptions/.watcher.log
autotranscribe run          # process pending files in the foreground
```

- **Nothing happens on drop**: check that the job is loaded (command above) and read `.watcher.log`.
- **A file landed in `failed/`**: read `failed/<name>.error.log`. To retry, move the file back into `~/Transcriptions`.
- **To reprocess a file**: move it from `processed/` back into `~/Transcriptions`. The new output gets a `_2` suffix.
- **Wrong language detected**: re-run `autotranscribe setup --language es`.
