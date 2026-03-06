from orchestra_agent.adapters.db import (
    InMemoryStepPlanRepository,
    InMemoryWorkflowRepository,
    PostgresAgentStateStore,
)
from orchestra_agent.domain import (
    AgentState,
    ExecutionRecord,
    ExecutionStatus,
    Step,
    StepPlan,
    Workflow,
)


def test_agent_state_store_append_execution() -> None:
    store = PostgresAgentStateStore()
    state = AgentState(run_id="run-1", workflow_id="wf-1")
    store.save(state)

    record = ExecutionRecord.pending(step_id="s1")
    record.mark_success(result={"ok": True})
    store.append_execution("run-1", record)

    loaded = store.load("run-1")
    assert loaded is not None
    assert loaded.execution_history[0].status == ExecutionStatus.SUCCESS


def test_in_memory_workflow_repository_latest() -> None:
    repo = InMemoryWorkflowRepository()
    repo.save(Workflow(workflow_id="wf-1", name="a", version=1, objective="o1"))
    repo.save(Workflow(workflow_id="wf-1", name="a", version=2, objective="o2"))

    latest = repo.get("wf-1")
    assert latest is not None
    assert latest.version == 2


def test_in_memory_step_plan_repository_latest() -> None:
    repo = InMemoryStepPlanRepository()
    step = Step(step_id="s1", name="a", description="a", tool_ref="excel.open_file")
    repo.save(StepPlan(step_plan_id="sp-1", workflow_id="wf-1", version=1, steps=[step]))
    repo.save(StepPlan(step_plan_id="sp-1", workflow_id="wf-1", version=2, steps=[step]))

    latest = repo.get("sp-1")
    assert latest is not None
    assert latest.version == 2
