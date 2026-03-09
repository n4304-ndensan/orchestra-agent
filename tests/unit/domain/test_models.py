from orchestra_agent.domain import (
    AgentState,
    BackupScope,
    DomainValidationError,
    ExecutionRecord,
    ExecutionStatus,
    Step,
    StepPlan,
    Workflow,
)


def test_workflow_with_feedback_increments_version() -> None:
    workflow = Workflow(
        workflow_id="wf-1",
        name="Excel Summary",
        version=1,
        objective="Summarize sales column C",
        reference_files=["docs/spec.pdf"],
    )
    updated = workflow.with_feedback("Need a summary sheet")

    assert updated.version == 2
    assert updated.reference_files == ["docs/spec.pdf"]
    assert updated.feedback_history == ["Need a summary sheet"]


def test_step_rejects_run_and_skip_both_true() -> None:
    try:
        Step(
            step_id="s1",
            name="invalid",
            description="invalid",
            tool_ref="excel.open_file",
            run=True,
            skip=True,
        )
    except DomainValidationError:
        pass
    else:
        raise AssertionError("Expected DomainValidationError")


def test_step_plan_topological_order() -> None:
    step_open = Step(
        step_id="open_file",
        name="Open file",
        description="Open workbook",
        tool_ref="excel.open_file",
        backup_scope=BackupScope.FILE,
    )
    step_read = Step(
        step_id="read_sheet",
        name="Read sheet",
        description="Read Sheet1",
        tool_ref="excel.read_sheet",
        depends_on=["open_file"],
    )
    step_sum = Step(
        step_id="calculate_totals",
        name="Calculate totals",
        description="Sum column C",
        tool_ref="excel.calculate_sum",
        depends_on=["read_sheet"],
    )
    plan = StepPlan(
        step_plan_id="sp-1",
        workflow_id="wf-1",
        version=1,
        steps=[step_sum, step_read, step_open],
    )

    assert plan.topologically_sorted_ids() == ["open_file", "read_sheet", "calculate_totals"]


def test_step_plan_rejects_cycle() -> None:
    step_1 = Step(
        step_id="a",
        name="a",
        description="a",
        tool_ref="excel.a",
        depends_on=["b"],
    )
    step_2 = Step(
        step_id="b",
        name="b",
        description="b",
        tool_ref="excel.b",
        depends_on=["a"],
    )

    try:
        StepPlan(step_plan_id="sp-cycle", workflow_id="wf-1", version=1, steps=[step_1, step_2])
    except DomainValidationError:
        pass
    else:
        raise AssertionError("Expected DomainValidationError")


def test_agent_state_tracks_last_error() -> None:
    state = AgentState(run_id="run-1")
    record = ExecutionRecord.pending(step_id="s1")
    record.mark_failed("boom")

    state.append_execution(record)

    assert state.last_error == "boom"
    assert state.execution_history[0].status == ExecutionStatus.FAILED

