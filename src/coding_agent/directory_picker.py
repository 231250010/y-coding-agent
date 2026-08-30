from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any


class DirectoryPickerError(RuntimeError):
    """Raised when the native directory picker cannot be opened or parsed."""


Runner = Callable[..., CompletedProcess[str]]
_PICKER_LOCK = threading.Lock()


def pick_directory(initial: str | None, *, runner: Runner = subprocess.run) -> str | None:
    """Open the OS directory picker in an isolated process.

    Tk must own the main thread on Windows. Running the dialog in a small child
    process also keeps Tk state out of the threaded local HTTP server.
    """

    command = [sys.executable, "-m", "coding_agent.directory_picker", "--child"]
    if initial:
        command.extend(("--initial", initial))
    options: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "check": False,
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
    }
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    with _PICKER_LOCK:
        try:
            result = runner(command, **options)
        except OSError as exc:
            raise DirectoryPickerError("无法启动本机目录选择器") from exc
    if result.returncode != 0:
        raise DirectoryPickerError("无法打开本机目录选择器")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DirectoryPickerError("目录选择器返回了无效结果") from exc
    if not isinstance(payload, dict) or payload.get("path") is not None and not isinstance(payload.get("path"), str):
        raise DirectoryPickerError("目录选择器返回了无效结果")
    raw_path = payload.get("path")
    if raw_path is None:
        return None
    selected = Path(raw_path).expanduser().resolve()
    if not selected.is_dir():
        raise DirectoryPickerError("选择的工作目录不存在")
    return str(selected)


def _choose_directory(initial: str | None) -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise DirectoryPickerError("当前 Python 环境不支持本机目录选择器") from exc
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initial_dir = Path(initial).expanduser() if initial else None
        selected = filedialog.askdirectory(
            parent=root,
            title="选择工作目录",
            initialdir=str(initial_dir) if initial_dir and initial_dir.is_dir() else None,
            mustexist=True,
        )
        root.destroy()
    except (OSError, tk.TclError) as exc:
        raise DirectoryPickerError("无法打开本机目录选择器") from exc
    return selected or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--initial")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.child:
        return 2
    try:
        selected = _choose_directory(args.initial)
    except DirectoryPickerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"path": selected}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
