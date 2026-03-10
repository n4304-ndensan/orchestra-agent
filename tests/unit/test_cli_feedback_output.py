from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from orchestra_agent.adapters.db import FilesystemStepPlanRepository, XmlWorkflowRepository
from orchestra_agent.cli import (
    _print_approval_preview,
    _print_failure_preview,
    _print_feedback_replan_summary,
    _print_plan,
    _print_result,
)
from orchestra_agent.domain import BackupScope, Step, StepPlan, Workflow
from orchestra_agent.runtime import RuntimeArtifacts


def test_feedback_summary_prints_regenerated_artifact_paths(capsys) -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        workflow_repo = XmlWorkflowRepository(base / "workflow")
        step_plan_repo = FilesystemStepPlanRepository(base / "plan")
        runtime = SimpleNamespace(
            workflow_repo=workflow_repo,
            step_plan_repo=step_plan_repo,
            artifacts=RuntimeArtifacts(
                workflow_root=base / "workflow",
                plan_root=base / "plan",
            ),
        )

        workflow_repo.save(
            Workflow(
                workflow_id="wf-feedback",
                name="Feedback workflow",
                version=2,
                objective="repair the plan",
            )
        )
        step_plan_repo.save(
            StepPlan(
                step_plan_id="sp-feedback",
                workflow_id="wf-feedback",
                version=2,
                steps=[
                    Step(
                        step_id="create_summary_sheet",
                        name="Create summary sheet",
                        description="Create the summary sheet again.",
                        tool_ref="excel.create_sheet",
                        resolved_input={"file": "summary.xlsx", "sheet": "Summary"},
                        backup_scope=BackupScope.NONE,
                    )
                ],
            )
        )

        _print_feedback_replan_summary(
            previous_run={
                "workflow_id": "wf-feedback",
                "workflow_version": 1,
                "step_plan_id": "sp-feedback-old",
                "step_plan_version": 1,
                "pending_approval": {
                    "stage": "POST_STEP",
                    "step_id": "create_summary_sheet",
                    "message": "Review pending.",
                },
            },
            updated_run={
                "workflow_id": "wf-feedback",
                "workflow_version": 2,
                "step_plan_id": "sp-feedback",
                "step_plan_version": 2,
                "approval_status": "PENDING",
                "metadata": {"feedback_step_id": "create_summary_sheet"},
            },
            runtime=runtime,
        )

        captured = capsys.readouterr().out
        assert "[feedback] create_summary_sheet -> replanned, approval pending" in captured
        assert "workflow_v2.xml" in captured
        assert "step_plan_v2.json" in captured
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_approval_preview_prints_processing_details(capsys) -> None:
    _print_approval_preview(
        "step plan v1 を実行します。",
        {
            "stage": "PLAN",
            "step_id": "__plan__",
            "message": "step plan v1 を実行します。",
            "details": [
                "plan     3 steps queued",
                "01. open_file | excel.open_file | file=input.xlsx",
                "02. read_sheet | excel.read_sheet | sheet=Sheet1",
            ],
        },
    )

    captured = capsys.readouterr().out
    assert "[approval] Plan review" in captured
    assert "step plan v1 を実行します。" in captured
    assert "plan     3 steps queued" in captured
    assert "01. open_file | excel.open_file | file=input.xlsx" in captured


def test_print_plan_formats_steps_readably(capsys) -> None:
    step_plan = StepPlan(
        step_plan_id="sp-hello",
        workflow_id="wf-hello",
        version=1,
        steps=[
            Step(
                step_id="create_excel_file",
                name="Create workbook",
                description="Create output/HelloWorld.xlsx.",
                tool_ref="excel.create_file",
                resolved_input={"path": "output/HelloWorld.xlsx"},
                backup_scope=BackupScope.NONE,
            ),
            Step(
                step_id="write_hello_world",
                name="Write A1",
                description="Write HelloWorld into A1.",
                tool_ref="excel.write_cells",
                resolved_input={
                    "path": "output/HelloWorld.xlsx",
                    "sheet_name": "Sheet1",
                    "cells": {"A1": "HelloWorld"},
                },
                backup_scope=BackupScope.NONE,
            ),
        ],
    )

    _print_plan(step_plan)

    captured = capsys.readouterr().out
    assert "Step Plan" in captured
    assert "id        sp-hello" in captured
    assert "01. Create workbook" in captured
    assert "input     file=output/HelloWorld.xlsx" in captured
    assert "sheet=Sheet1" in captured


def test_print_result_formats_failure_readably(capsys) -> None:
    _print_result(
        {
            "run_id": "run-cli",
            "approval_status": "REJECTED",
            "current_step_id": None,
            "pending_approval": None,
            "last_error": "MCP error for tools/call: {'code': -32002, 'message': \"'file'\"}",
            "execution_history": [
                {
                    "step_id": "create_excel_file",
                    "status": "FAILED",
                }
            ],
        },
        warning=None,
    )

    captured = capsys.readouterr().out
    assert "Run Result" in captured
    assert "status    REJECTED" in captured
    assert "error     MCP error for tools/call" in captured
    assert "create_excel_file  FAILED" in captured


def test_failure_preview_prints_recovery_hint(capsys) -> None:
    _print_failure_preview("boom")

    captured = capsys.readouterr().out
    assert "[failure] Run failed" in captured
    assert "error     boom" in captured
    assert "retry / no / feedback" in captured
