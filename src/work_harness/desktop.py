from __future__ import annotations

import ctypes
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from work_harness.app_paths import prepare_application_environment, user_data_root

_MUTEX_HANDLE: Any | None = None
_MUTEX_NAME = "Local\\WorkHarnessDesktop"


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


def _open_browser_when_ready(url: str) -> None:
    if _wait_for_server(url):
        webbrowser.open(url)


def _run_desktop(project_root: Path | None = None) -> int:
    import uvicorn

    from work_harness.api.app import create_app
    from work_harness.config.loader import load_app_config

    config = load_app_config(project_root)
    port = config.api.port
    url = f"http://127.0.0.1:{port}"
    if not _acquire_windows_mutex():
        webbrowser.open(url)
        return 0

    browser = threading.Thread(
        target=_open_browser_when_ready,
        args=(url,),
        name="work-harness-browser",
        daemon=True,
    )
    browser.start()
    print(f"Work Harness data: {user_data_root()}")
    print(f"Opening {url} (press Ctrl+C to stop)")
    try:
        uvicorn.run(create_app(project_root), host="127.0.0.1", port=port)
    finally:
        _release_windows_mutex()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    prepare_application_environment()
    if arguments:
        from work_harness.cli import main as cli_main

        return cli_main(arguments)
    return _run_desktop()


if __name__ == "__main__":
    raise SystemExit(main())
