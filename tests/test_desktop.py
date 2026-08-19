from __future__ import annotations

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
