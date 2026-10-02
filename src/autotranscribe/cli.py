"""Command line entry point: `autotranscribe run | setup | uninstall`."""

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from autotranscribe.worker import DEFAULT_MODEL, DEFAULT_WATCH_DIR, run

LABEL = "com.gianniballerini.autotranscribe"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
# launchd jobs start with a minimal PATH; ffmpeg usually lives in Homebrew's bin.
JOB_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PLUGIN_NAME = "autotranscribe.2s.py"
FALLBACK_PLUGIN_DIR = Path.home() / "Library/Application Support/autotranscribe/swiftbar"


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def swiftbar_plugin_dir() -> Path | None:
    """SwiftBar's configured plugin folder, or None if SwiftBar was never set up."""
    result = subprocess.run(["defaults", "read", "com.ameba.SwiftBar", "PluginDirectory"],
                            capture_output=True, text=True)
    value = result.stdout.strip()
    return Path(value).expanduser() if result.returncode == 0 and value else None


def build_plist(watch_dir: Path, model: str, language: str | None) -> dict:
    args = [sys.executable, "-m", "autotranscribe", "run", "--watch-dir", str(watch_dir)]
    if model != DEFAULT_MODEL:
        args += ["--model", model]
    if language:
        args += ["--language", language]
    log_path = str(watch_dir / ".watcher.log")
    return {
        "Label": LABEL,
        "ProgramArguments": args,
        "WatchPaths": [str(watch_dir)],
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": JOB_PATH, "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


def install_plugin() -> Path:
    """Write the SwiftBar plugin with a shebang pointing at this interpreter."""
    source = resources.files("autotranscribe").joinpath("swiftbar_plugin.py").read_text()
    body = source.split("\n", 1)[1]
    target_dir = swiftbar_plugin_dir() or FALLBACK_PLUGIN_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / PLUGIN_NAME
    if target.is_symlink():
        target.unlink()
    target.write_text(f"#!{sys.executable}\n{body}")
    target.chmod(0o755)
    return target


def cmd_setup(args: argparse.Namespace) -> int:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        print("autotranscribe needs an Apple Silicon Mac (MLX).", file=sys.stderr)
        return 1
    search_path = os.pathsep.join([JOB_PATH, os.environ.get("PATH", "")])
    if not shutil.which("ffmpeg", path=search_path):
        print("ffmpeg not found. Install it first: brew install ffmpeg", file=sys.stderr)
        return 1

    watch_dir = Path(args.watch_dir).expanduser().resolve()
    watch_dir.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_bytes(plistlib.dumps(build_plist(watch_dir, args.model, args.language)))

    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")  # fine if it was not loaded
    result = launchctl("bootstrap", domain, str(PLIST_PATH))
    if result.returncode != 0:
        print(f"launchctl bootstrap failed:\n{result.stderr}", file=sys.stderr)
        return 1
    print(f"Watching {watch_dir} (launchd job {LABEL})")
    print(f"Log: {watch_dir / '.watcher.log'}")
    print("The Whisper model (~1.5 GB) downloads on the first transcription.")

    if not args.no_swiftbar:
        plugin = install_plugin()
        print(f"Menu bar plugin: {plugin}")
        if plugin.parent == FALLBACK_PLUGIN_DIR:
            print("  SwiftBar is not configured. Install it (brew install --cask swiftbar)")
            print(f"  and set its plugin folder to: {FALLBACK_PLUGIN_DIR}")
    return 0


def cmd_uninstall(_: argparse.Namespace) -> int:
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    PLIST_PATH.unlink(missing_ok=True)
    for plugin_dir in filter(None, [swiftbar_plugin_dir(), FALLBACK_PLUGIN_DIR]):
        (plugin_dir / PLUGIN_NAME).unlink(missing_ok=True)
    print("Removed the launchd job and the menu bar plugin. Your transcripts were left untouched.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    run(Path(args.watch_dir), args.model, args.language)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="autotranscribe",
                                     description="Transcribe all media files dropped into a folder.")
    sub = parser.add_subparsers(dest="command")

    def add_job_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--watch-dir", default=str(DEFAULT_WATCH_DIR),
                       help=f"Folder to watch (default: {DEFAULT_WATCH_DIR})")
        p.add_argument("--model", default=DEFAULT_MODEL, help="MLX Whisper model (Hugging Face repo)")
        p.add_argument("--language", default=None,
                       help="Force a language code (e.g. 'es', 'en'). Auto-detect if omitted.")

    p_run = sub.add_parser("run", help="Process pending files once (what the watcher runs)")
    add_job_options(p_run)
    p_run.set_defaults(func=cmd_run)

    p_setup = sub.add_parser("setup", help="Install the background watcher and menu bar plugin")
    add_job_options(p_setup)
    p_setup.add_argument("--no-swiftbar", action="store_true", help="Skip the menu bar plugin")
    p_setup.set_defaults(func=cmd_setup)

    p_uninstall = sub.add_parser("uninstall", help="Remove the background watcher and menu bar plugin")
    p_uninstall.set_defaults(func=cmd_uninstall)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(2)
    sys.exit(args.func(args))
