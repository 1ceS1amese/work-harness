"""Preparation must not load credentials, run graphs, or masquerade as measured results."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_harness import cli


def test_prepare_comparison_is_offline_and_preserves_existing_report(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("preparation must not load runtime or provider configuration")

    monkeypatch.setattr(cli, "build_runtime", forbidden)
    monkeypatch.setattr(cli, "load_app_config", forbidden)
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-must-not-appear")
    measured = tmp_path / "existing-evaluation.json"
    measured.write_text('{"measured": true}\n')
    monkeypatch.setenv("WORK_HARNESS_EVALUATION_OUTPUT", str(measured))
    assert cli.main(["--project-root", str(tmp_path), "eval", "--prepare-comparison"]) == 0
    stdout = capsys.readouterr().out
    assert "test-secret-must-not-appear" not in stdout
    report = json.loads(stdout)
    assert report["status"] == "awaiting_provider"
    assert not report["quality_claim"] and not report["eligible_for_default_switch"]
    assert set(report["results"]) == {"legacy", "blackboard"}
    for result in report["results"].values():
        assert result["status"] == "not_run"
        assert all(value is None for key, value in result.items() if key != "status")
    saved = tmp_path / "evaluation/reports/comparison.pending.json"
    assert json.loads(saved.read_text()) == report
    assert measured.read_text() == '{"measured": true}\n'
    # A second preparation must not erase a handoff someone has already filled in.
    saved.write_text('{"provider": {"name": "user-provided"}}\n')
    with pytest.raises(FileExistsError):
        cli.main(["--project-root", str(tmp_path), "eval", "--prepare-comparison"])
    assert saved.read_text() == '{"provider": {"name": "user-provided"}}\n'


def test_prepare_comparison_rejects_execution_cases_without_creating_output(tmp_path):
    output = tmp_path / "prepared.json"
    with pytest.raises(ValueError, match="--cases is for execution"):
        cli.main(
            ["eval", "--prepare-comparison", "--cases", "cases.jsonl", "--output", str(output)]
        )
    assert not output.exists()


def test_existing_mock_evaluation_remains_executable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WORK_HARNESS_PROFILE", "local")
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "smoke.json"
    assert cli.main(["--project-root", str(root), "eval", "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["evaluation_scope"] == "mock_provider_workflow_smoke"
    assert report["case_count"] == 10 and report["bad_case_count"] == 0
    assert report["quality_claim"] is False
    assert json.loads(output.read_text()) == report
