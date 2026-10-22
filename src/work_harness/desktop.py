from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from work_harness.app_paths import ApplicationPaths, prepare_application_environment, user_data_root

_MUTEX_HANDLE: Any | None = None
_MUTEX_NAME = "Local\\WorkHarnessDesktop"
_DESKTOP_LOG_STREAM: TextIO | None = None
_WINDOW_TITLE = "Work Harness"


def _acquire_windows_mutex() -> bool:
    """Keep a single desktop server per Windows login session."""

    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.GetLastError.restype = ctypes.c_ulong
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "unable to create the desktop mutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = handle
    return True


def _release_windows_mutex() -> None:
    global _MUTEX_HANDLE
    if sys.platform == "win32" and _MUTEX_HANDLE:
        kernel32 = ctypes.windll.kernel32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle(_MUTEX_HANDLE)
        _MUTEX_HANDLE = None


def _activate_existing_window() -> bool:
    """Restore and focus the existing desktop window after a second launch."""

    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    handle = user32.FindWindowW(None, _WINDOW_TITLE)
    if not handle:
        return False
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    user32.SetForegroundWindow(handle)
    return True


def _wait_for_server(url: str, *, timeout_seconds: float = 30) -> bool:
    deadline = time.monotonic() + timeout_seconds
    health_url = f"{url}/health/live"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    return False


def _prepare_windowed_output(paths: ApplicationPaths) -> None:
    """Give logging a real stream when PyInstaller uses the Windows GUI subsystem."""

    global _DESKTOP_LOG_STREAM
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path = paths.logs / "desktop.log"
    _DESKTOP_LOG_STREAM = log_path.open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _DESKTOP_LOG_STREAM
    if sys.stderr is None:
        sys.stderr = _DESKTOP_LOG_STREAM


def _show_error(message: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, _WINDOW_TITLE, 0x10)
        return
    if sys.stderr is not None:
        print(message, file=sys.stderr)


def _open_desktop_window(url: str) -> None:
    """Open the application in a native WebView2 window and block until it closes."""

    try:
        import webview

        renderer: list[str] = []

        def require_webview2(selected_renderer: str) -> bool:
            renderer.append(selected_renderer)
            return selected_renderer == "edgechromium"

        window = webview.create_window(
            _WINDOW_TITLE,
            url,
            width=1440,
            height=900,
            min_size=(1024, 700),
            background_color="#f4f4f5",
            text_select=True,
        )
        if window is None:
            raise RuntimeError("桌面窗口初始化被取消")
        window.events.initialized += require_webview2
        webview.start(
            gui="edgechromium",
            private_mode=False,
            storage_path=str(user_data_root() / "webview"),
        )
        if renderer != ["edgechromium"]:
            raise RuntimeError("未检测到 Microsoft Edge WebView2 Runtime")
    except Exception as exc:
        raise RuntimeError(
            "无法创建 Work Harness 桌面窗口。请安装 Microsoft Edge WebView2 Runtime 后重试。"
        ) from exc


def _stop_server(server: Any, server_thread: threading.Thread) -> None:
    server.should_exit = True
    server_thread.join(timeout=10)
    if server_thread.is_alive():
        server.force_exit = True
        server_thread.join(timeout=5)


def _serve_with_desktop_window(server: Any, url: str) -> int:
    server_thread = threading.Thread(
        target=server.run,
        name="work-harness-server",
        daemon=True,
    )
    server_thread.start()
    ready = _wait_for_server(url)
    started = bool(getattr(server, "started", True))
    if not ready or not started or not server_thread.is_alive():
        _stop_server(server, server_thread)
        _show_error(
            "Work Harness 本地服务启动失败。请确认端口未被占用，并查看数据目录中的 "
            "logs\\desktop.log。"
        )
        return 1
    try:
        _open_desktop_window(url)
    finally:
        _stop_server(server, server_thread)
    return 0


def _run_desktop(project_root: Path | None = None) -> int:
    import uvicorn

    from work_harness.api.app import create_app
    from work_harness.config.loader import load_app_config

    config = load_app_config(project_root)
    port = config.api.port
    url = f"http://127.0.0.1:{port}"
    if not _acquire_windows_mutex():
        if not _wait_for_server(url, timeout_seconds=5):
            _show_error("已检测到 Work Harness 进程，但无法连接到桌面服务。")
            return 1
        deadline = time.monotonic() + 5
        while not _activate_existing_window() and time.monotonic() < deadline:
            time.sleep(0.1)
        return 0
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(project_root),
                host="127.0.0.1",
                port=port,
                access_log=False,
            )
        )
        return _serve_with_desktop_window(server, url)
    finally:
        _release_windows_mutex()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    paths = prepare_application_environment()
    _prepare_windowed_output(paths)
    if arguments:
        from work_harness.cli import main as cli_main

        return cli_main(arguments)
    return _run_desktop()


def _entrypoint() -> int:
    try:
        return main()
    except Exception as exc:
        logging.getLogger(__name__).exception("desktop startup failed")
        log_path = user_data_root() / "logs" / "desktop.log"
        _show_error(f"Work Harness 启动失败：{exc}\n\n详细日志：{log_path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
