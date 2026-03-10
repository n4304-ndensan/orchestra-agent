import shutil
from pathlib import Path
from uuid import uuid4

from orchestra_agent.adapters.planner import LlmPlanner
from orchestra_agent.adapters.policy import DefaultPolicyEngine
from orchestra_agent.adapters.snapshot import FilesystemSnapshotManager
from orchestra_agent.domain import BackupScope, RiskLevel, Step, StepPlan, Workflow


def test_llm_planner_generates_excel_step_plan() -> None:
    workflow = Workflow(
        workflow_id="wf-1",
        name="Excel summary",
        version=1,
        objective=(
            "Open Excel file sales.xlsx, calculate totals for column C, "
            "create a summary sheet, and export as summary.xlsx"
        ),
    )
    planner = LlmPlanner()
    plan = planner.compile_step_plan(workflow)

    assert [step.step_id for step in plan.ordered_steps()] == [
        "open_file",
        "read_sheet",
        "calculate_totals",
        "create_summary_sheet",
        "write_summary",
        "save_file",
    ]
    assert plan.step_map()["save_file"].resolved_input["output"] == "summary.xlsx"


def test_llm_planner_supports_japanese_objective() -> None:
    workflow = Workflow(
        workflow_id="wf-jp",
        name="JP Excel summary",
        version=1,
        objective="sales.xlsxのC列を集計してsummary.xlsxへ",
    )
    planner = LlmPlanner()
    plan = planner.compile_step_plan(workflow)

    assert plan.step_map()["calculate_totals"].resolved_input["column"] == "C"
    assert plan.step_map()["save_file"].resolved_input["output"] == "summary.xlsx"


def test_default_policy_engine_returns_pending_for_high_risk_step() -> None:
    plan = StepPlan(
        step_plan_id="sp-1",
        workflow_id="wf-1",
        version=1,
        steps=[
            Step(
                step_id="s1",
                name="low",
                description="low",
                tool_ref="excel.open_file",
            ),
            Step(
                step_id="s2",
                name="high",
                description="high",
                tool_ref="excel.save_file",
                depends_on=["s1"],
                risk_level=RiskLevel.HIGH,
            ),
        ],
    )
    engine = DefaultPolicyEngine()
    result = engine.evaluate(plan)

    assert result.approval_status.value == "PENDING"
    assert result.step_plan.step_map()["s1"].requires_approval is True
    assert any("elevated risk level" in reason for reason in result.reasons)
    assert any("first executable checkpoint" in reason for reason in result.reasons)


def test_filesystem_snapshot_manager_restores_file() -> None:
    base = Path(".tmp-tests") / uuid4().hex
    base.mkdir(parents=True, exist_ok=False)
    try:
        snapshots_dir = base / "snapshots"
        manager = FilesystemSnapshotManager(base_snapshot_dir=snapshots_dir, workspace_root=base)
        target_file = base / "sales.xlsx"
        target_file.write_text("original", encoding="utf-8")

        snapshot_ref = manager.create_snapshot(
            scope=BackupScope.FILE,
            metadata={"file_path": str(target_file)},
        )
        target_file.write_text("changed", encoding="utf-8")
        manager.restore_snapshot(snapshot_ref)

        assert target_file.read_text(encoding="utf-8") == "original"
    finally:
        shutil.rmtree(base, ignore_errors=True)
