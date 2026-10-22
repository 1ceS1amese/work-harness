"""Command-line admission keeps explicit acceptance and works with either default."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

import pytest
from test_backup_restore import project as project
from test_blackboard_inputs import human_request

from work_harness import cli, desktop


@pytest.mark.parametrize("default_engine", ("legacy", "blackboard"))
def test_cli_selects_saved_draft_without_rewriting_acceptance(
    project, monkeypatch, capsys, default_engine
):
    monkeypatch.setenv("WORK_HARNESS_WORKFLOW_ENGINE", default_engine)
    arguments = ["--project-root", str(project), "run", "--goal", "起草项目说明"]
    if default_engine == "legacy":
        arguments += ["--engine", "blackboard"]
    assert cli.main([*arguments, "--acceptance-template", "saved-draft"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["engine"] == "blackboard" and result["status"] == "succeeded"
    assert "尚未核验" in result["output"]
    with closing(sqlite3.connect(project / "var/runs.sqlite")) as db:
        row = db.execute("SELECT execution FROM run_index").fetchone()
    assert json.loads(row[0])["engine"] == "blackboard"


def test_cli_human_criterion_remains_unsatisfied_until_confirmation(project, tmp_path, capsys):
    spec = tmp_path / "acceptance.json"
    spec.write_text(human_request().blackboard.model_dump_json(), encoding="utf-8")
    assert (
        cli.main(
            [
                "--project-root",
                str(project),
                "run",
                "--goal",
                "确认上线决定",
                "--engine",
                "blackboard",
                "--blackboard-spec",
                str(spec),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["engine"] == "blackboard" and result["status"] == "waiting_human"
    assert result["pending_intents"]


def test_cli_refuses_implicit_acceptance_before_starting_runtime(project, monkeypatch):
    monkeypatch.setenv("WORK_HARNESS_WORKFLOW_ENGINE", "blackboard")
    with pytest.raises(ValueError, match="require --blackboard-spec"):
        cli.main(["--project-root", str(project), "run", "--goal", "调查市场"])
    assert not (project / "var").exists()
    with pytest.raises(ValueError, match="requires --engine blackboard"):
        cli.main(
            [
                "--project-root",
                str(project),
                "run",
                "--goal",
                "调查市场",
                "--engine",
                "legacy",
                "--acceptance-template",
                "saved-draft",
            ]
        )
    assert not (project / "var").exists()


def test_desktop_cli_keeps_blackboard_with_other_user_data(project, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    data_root = tmp_path / "desktop-data"
    for key in (
        "WORK_HARNESS_CHECKPOINT_PATH",
        "WORK_HARNESS_RUN_STORE_PATH",
        "WORK_HARNESS_PROVIDER_STORE_PATH",
        "WORK_HARNESS_BLACKBOARD_PATH",
        "WORK_HARNESS_SETTINGS_PATH",
        "WORK_HARNESS_EVALUATION_OUTPUT",
        "WORK_HARNESS_SCHEMA_OUTPUT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WORK_HARNESS_DATA_ROOT", str(data_root))
    assert (
        desktop.main(
            [
                "--project-root",
                str(project),
                "run",
                "--goal",
                "起草项目说明",
                "--engine",
                "blackboard",
                "--acceptance-template",
                "saved-draft",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    for name in ("blackboard.sqlite", "checkpoints.sqlite", "runs.sqlite", "providers.sqlite"):
        assert (data_root / "data" / name).is_file()
    assert not (project / "var").exists()
