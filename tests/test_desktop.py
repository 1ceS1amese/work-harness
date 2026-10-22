from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

from work_harness import desktop


def test_desktop_arguments_delegate_to_cli(monkeypatch, tmp_path) -> None:
    captured: list[list[str]] = []
    monkeypatch.setenv("WORK_HARNESS_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "work_harness.cli.main",
        lambda arguments: captured.append(list(arguments)) or 17,
    )

    result = desktop.main(["check-config"])

    assert result == 17
    assert captured == [["check-config"]]
    assert (tmp_path / "data").is_dir()


def test_wait_for_server_returns_false_after_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        desktop.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    assert not desktop._wait_for_server("http://127.0.0.1:1", timeout_seconds=0)


def test_desktop_window_owns_server_lifecycle(monkeypatch) -> None:
    window_opened: list[str] = []

    class FakeServer:
        started = True
        should_exit = False
        force_exit = False

        def run(self) -> None:
            while not self.should_exit and not self.force_exit:
                time.sleep(0.001)

    server = FakeServer()
    monkeypatch.setattr(desktop, "_wait_for_server", lambda url: True)
    monkeypatch.setattr(
        desktop,
        "_open_desktop_window",
        lambda url: window_opened.append(url),
    )

    result = desktop._serve_with_desktop_window(server, "http://127.0.0.1:8000")

    assert result == 0
    assert window_opened == ["http://127.0.0.1:8000"]
    assert server.should_exit
    assert not any(thread.name == "work-harness-server" for thread in threading.enumerate())


def test_desktop_reports_server_start_failure(monkeypatch) -> None:
    messages: list[str] = []

    class FailedServer:
        started = False
        should_exit = False
        force_exit = False

        def run(self) -> None:
            return

    monkeypatch.setattr(desktop, "_wait_for_server", lambda url: False)
    monkeypatch.setattr(desktop, "_show_error", messages.append)

    result = desktop._serve_with_desktop_window(
        FailedServer(),
        "http://127.0.0.1:8000",
    )

    assert result == 1
    assert messages and "启动失败" in messages[0]


def test_native_window_uses_persistent_webview2(monkeypatch, tmp_path) -> None:
    calls: dict[str, object] = {}

    class InitializedEvent:
        handler = None

        def __iadd__(self, handler):
            self.handler = handler
            return self

    initialized = InitializedEvent()
    window = SimpleNamespace(events=SimpleNamespace(initialized=initialized))

    def create_window(*args, **kwargs):
        calls["create"] = (args, kwargs)
        return window

    def start(**kwargs):
        calls["start"] = kwargs
        assert initialized.handler is not None
        assert initialized.handler("edgechromium")

    fake_webview = SimpleNamespace(create_window=create_window, start=start)
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setenv("WORK_HARNESS_DATA_ROOT", str(tmp_path))

    desktop._open_desktop_window("http://127.0.0.1:8000")

    assert calls["start"] == {
        "gui": "edgechromium",
        "private_mode": False,
        "storage_path": str(tmp_path / "webview"),
    }


def test_native_window_rejects_legacy_renderer(monkeypatch, tmp_path) -> None:
    class InitializedEvent:
        handler = None

        def __iadd__(self, handler):
            self.handler = handler
            return self

    initialized = InitializedEvent()
    window = SimpleNamespace(events=SimpleNamespace(initialized=initialized))

    def start(**kwargs):
        assert initialized.handler is not None
        assert not initialized.handler("mshtml")

    fake_webview = SimpleNamespace(create_window=lambda *args, **kwargs: window, start=start)
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setenv("WORK_HARNESS_DATA_ROOT", str(tmp_path))

    try:
        desktop._open_desktop_window("http://127.0.0.1:8000")
    except RuntimeError as exc:
        assert "WebView2 Runtime" in str(exc)
    else:
        raise AssertionError("legacy renderer should be rejected")
