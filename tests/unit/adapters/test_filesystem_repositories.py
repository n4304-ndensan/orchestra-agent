from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from orchestra_agent.adapters.db import FilesystemStepPlanRepository, XmlWorkflowRepository
from orchestra_agent.domain import BackupScope, Step, StepPlan, Workflow


def test_xml_workflow_repository_save_and_load() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        repo = XmlWorkflowRepository(base / "workflow")
        workflow = Workflow(
            workflow_id="wf-xml",
            name="Excel summary",
            version=1,
            objective="sales.xlsxのC列を集計してsummary.xlsxへ",
            constraints=["Do not modify source values"],
            success_criteria=["summary.xlsx is generated"],
        )
        repo.save(workflow)

        loaded = repo.get("wf-xml")
        assert loaded is not None
        assert loaded.objective == workflow.objective
        assert (base / "workflow" / "wf-xml" / "workflow.xml").is_file()

        updated = workflow.with_feedback("write_summary failed once")
        repo.save(updated)
        loaded_v2 = repo.get("wf-xml", version=2)
        assert loaded_v2 is not None
        assert loaded_v2.feedback_history[-1] == "write_summary failed once"
        assert (base / "workflow" / "wf-xml" / "feedback" / "feedback_v2.txt").is_file()

        repo.lock_workflow("wf-xml")
        assert repo.is_locked("wf-xml") is True
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_filesystem_step_plan_repository_save_and_load() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        repo = FilesystemStepPlanRepository(base / "plan")
        step = Step(
            step_id="open_file",
            name="open",
            description="open",
            tool_ref="excel.open_file",
            resolved_input={"file": "sales.xlsx"},
            backup_scope=BackupScope.NONE,
        )
        plan = StepPlan(
            step_plan_id="sp-filesystem",
            workflow_id="wf-xml",
            version=1,
            steps=[step],
        )
        repo.save(plan)

        loaded = repo.get("sp-filesystem")
        assert loaded is not None
        assert loaded.workflow_id == "wf-xml"
        assert loaded.steps[0].tool_ref == "excel.open_file"
        assert (
            base / "plan" / "wf-xml" / "sp-filesystem" / "step_plan_latest.json"
        ).is_file()
        assert (
            base / "plan" / "wf-xml" / "sp-filesystem" / "step_plan_latest.xml"
        ).is_file()

        repo.lock_step_plan("wf-xml", "sp-filesystem")
        assert repo.is_locked("wf-xml", "sp-filesystem") is True
    finally:
        shutil.rmtree(base, ignore_errors=True)
