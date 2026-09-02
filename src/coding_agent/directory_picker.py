from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any


class DirectoryPickerError(RuntimeError):
    """Raised when the native directory picker cannot be opened or parsed."""


Runner = Callable[..., CompletedProcess[str]]
_PICKER_LOCK = threading.Lock()


def pick_directory(initial: str | None, *, runner: Runner = subprocess.run) -> str | None:
    """Open the system picker in a lightweight, UI-owning child process.

    The HTTP request thread must not own a Windows modal dialog: services started
    without a foreground window can leave IFileDialog blocked and invisible.
    The child main thread owns the dialog instead. On Windows it uses native COM
    without importing Tk; Tk remains a fallback when COM is unavailable.
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
    return _resolve_selected_directory(payload.get("path"))


def _resolve_selected_directory(raw_path: str | None) -> str | None:
    if raw_path is None:
        return None
    selected = Path(raw_path).expanduser().resolve()
    if not selected.is_dir():
        raise DirectoryPickerError("选择的工作目录不存在")
    return str(selected)


def _choose_directory_windows(initial: str | None) -> str | None:
    """Select a filesystem folder through Windows' native IFileOpenDialog."""

    if sys.platform != "win32":
        raise DirectoryPickerError("Windows 原生目录选择器仅支持 Windows")

    try:
        import ctypes
        from ctypes import wintypes
    except ImportError as exc:  # pragma: no cover - ctypes ships with CPython
        raise DirectoryPickerError("当前 Python 环境不支持 Windows 目录选择器") from exc

    class GUID(ctypes.Structure):
        _fields_ = (
            ("data1", ctypes.c_ulong),
            ("data2", ctypes.c_ushort),
            ("data3", ctypes.c_ushort),
            ("data4", ctypes.c_ubyte * 8),
        )

        @classmethod
        def parse(cls, value: str) -> "GUID":
            return cls.from_buffer_copy(uuid.UUID(value).bytes_le)

    def code(result: int) -> int:
        return ctypes.c_ulong(result).value

    def failed(result: int) -> bool:
        return bool(code(result) & 0x80000000)

    def check(result: int, action: str) -> None:
        if failed(result):
            raise DirectoryPickerError(
                f"Windows 目录选择器{action}失败 (0x{code(result):08X})"
            )

    def method(
        interface: Any, index: int, result_type: Any, *argument_types: Any
    ) -> Any:
        table = ctypes.cast(
            interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        return ctypes.WINFUNCTYPE(
            result_type, ctypes.c_void_p, *argument_types
        )(table[index])

    ole32 = ctypes.OleDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = ()
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = (
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole32.CoTaskMemFree.restype = None
    shell32.SHCreateItemFromParsingName.argtypes = (
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long

    clsid_file_open_dialog = GUID.parse("dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7")
    iid_file_open_dialog = GUID.parse("d57c7288-d4ad-4768-be02-9d969532d960")
    iid_shell_item = GUID.parse("43826d1e-e718-42ee-bc55-a1e261c37bfe")

    coinit_apartment_threaded = 0x2
    clsctx_inproc_server = 0x1
    rpc_e_changed_mode = 0x80010106
    error_cancelled = 0x800704C7
    fos_pick_folders = 0x20
    fos_force_file_system = 0x40
    fos_path_must_exist = 0x800
    fos_dont_add_to_recent = 0x02000000
    sigdn_file_system_path = 0x80058000

    initialized = False
    dialog = ctypes.c_void_p()
    initial_item = ctypes.c_void_p()
    result_item = ctypes.c_void_p()
    display_name = wintypes.LPWSTR()
    try:
        initialize_result = ole32.CoInitializeEx(None, coinit_apartment_threaded)
        if code(initialize_result) == rpc_e_changed_mode:
            raise DirectoryPickerError("当前线程的 Windows COM 模式不兼容")
        check(initialize_result, "初始化")
        initialized = True

        check(
            ole32.CoCreateInstance(
                ctypes.byref(clsid_file_open_dialog),
                None,
                clsctx_inproc_server,
                ctypes.byref(iid_file_open_dialog),
                ctypes.byref(dialog),
            ),
            "创建",
        )

        get_options = method(
            dialog, 10, ctypes.c_long, ctypes.POINTER(wintypes.DWORD)
        )
        set_options = method(dialog, 9, ctypes.c_long, wintypes.DWORD)
        set_title = method(dialog, 17, ctypes.c_long, wintypes.LPCWSTR)
        options = wintypes.DWORD()
        check(get_options(dialog, ctypes.byref(options)), "读取选项")
        options.value |= (
            fos_pick_folders
            | fos_force_file_system
            | fos_path_must_exist
            | fos_dont_add_to_recent
        )
        check(set_options(dialog, options), "设置选项")
        check(set_title(dialog, "选择工作目录"), "设置标题")

        initial_path = Path(initial).expanduser().resolve() if initial else None
        if initial_path is not None and initial_path.is_dir():
            create_item_result = shell32.SHCreateItemFromParsingName(
                str(initial_path),
                None,
                ctypes.byref(iid_shell_item),
                ctypes.byref(initial_item),
            )
            if not failed(create_item_result):
                set_folder = method(dialog, 12, ctypes.c_long, ctypes.c_void_p)
                set_folder(dialog, initial_item)

        show = method(dialog, 3, ctypes.c_long, wintypes.HWND)
        show_result = show(dialog, None)
        if code(show_result) == error_cancelled:
            return None
        check(show_result, "打开")

        get_result = method(
            dialog, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
        )
        check(get_result(dialog, ctypes.byref(result_item)), "读取结果")
        get_display_name = method(
            result_item,
            5,
            ctypes.c_long,
            ctypes.c_uint,
            ctypes.POINTER(wintypes.LPWSTR),
        )
        check(
            get_display_name(
                result_item,
                sigdn_file_system_path,
                ctypes.byref(display_name),
            ),
            "解析路径",
        )
        return display_name.value
    except OSError as exc:
        raise DirectoryPickerError("无法打开 Windows 原生目录选择器") from exc
    finally:
        if display_name:
            ole32.CoTaskMemFree(ctypes.cast(display_name, ctypes.c_void_p))
        for interface in (result_item, initial_item, dialog):
            if interface.value:
                release = method(interface, 2, wintypes.ULONG)
                release(interface)
        if initialized:
            ole32.CoUninitialize()


def _choose_directory(initial: str | None) -> str | None:
    if sys.platform == "win32":
        try:
            return _choose_directory_windows(initial)
        except DirectoryPickerError:
            pass
    return _choose_directory_tk(initial)


def _choose_directory_tk(initial: str | None) -> str | None:
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
