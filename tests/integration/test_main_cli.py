from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

from orchestra_agent.cli import main as run_cli


def test_main_runs_with_single_command_objective() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source = base / "sales.xlsx"
        source.write_text("seed", encoding="utf-8")

        exit_code = run_cli(
            [
                "sales.xlsxのC列を集計してsummary.xlsxへ",
                "--workspace",
                str(base),
                "--run-id",
                "run-main-test",
                "--no-print-plan",
            ]
        )

        assert exit_code == 0
        assert (base / "summary.xlsx").is_file()
        assert any((base / "workflow").rglob("workflow.xml"))
        assert any((base / "plan").rglob("step_plan_latest.json"))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_main_runs_using_workflow_xml_import() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source = base / "sales.xlsx"
        source.write_text("seed", encoding="utf-8")

        imported_xml = base / "workflow_source.xml"
        imported_xml.write_text(
            "\n".join(
                [
                    '<?xml version="1.0" encoding="utf-8"?>',
                    '<workflow id="wf-imported" version="1">',
                    "  <name>Imported Workflow</name>",
                    "  <objective>sales.xlsxのC列を集計してsummary.xlsxへ</objective>",
                    "  <constraints>",
                    "    <item>Do not alter source workbook</item>",
                    "  </constraints>",
                    "  <success_criteria>",
                    "    <item>summary.xlsx exists</item>",
                    "  </success_criteria>",
                    "  <feedback_history />",
                    "</workflow>",
                ]
            ),
            encoding="utf-8",
        )

        exit_code = run_cli(
            [
                "--workflow-xml",
                str(imported_xml),
                "--workspace",
                str(base),
                "--run-id",
                "run-main-xml",
                "--no-print-plan",
            ]
        )

        assert exit_code == 0
        assert (base / "summary.xlsx").is_file()
        assert (base / "workflow" / "wf-imported" / "workflow.xml").is_file()
        assert any((base / "plan" / "wf-imported").rglob("step_plan_latest.json"))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_main_generates_fresh_run_ids_when_omitted() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        source = base / "sales.xlsx"
        source.write_text("seed", encoding="utf-8")

        first_exit_code = run_cli(
            [
                "sales.xlsxのC列を集計してsummary.xlsxへ",
                "--workspace",
                str(base),
                "--no-print-plan",
            ]
        )
        second_exit_code = run_cli(
            [
                "sales.xlsxのC列を集計してsummary.xlsxへ",
                "--workspace",
                str(base),
                "--no-print-plan",
            ]
        )

        assert first_exit_code == 0
        assert second_exit_code == 0

        state_files = sorted((base / ".orchestra_state" / "runs").glob("*.json"))
        assert len(state_files) == 2
        assert state_files[0].name != state_files[1].name

        states = [
            json.loads(state_file.read_text(encoding="utf-8"))
            for state_file in state_files
        ]
        assert all(state["approval_status"] == "APPROVED" for state in states)
        assert all(len(state["execution_history"]) == 6 for state in states)
        assert all(
            all(record["status"] == "SUCCESS" for record in state["execution_history"])
            for state in states
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)
