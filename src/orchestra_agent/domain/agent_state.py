from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from orchestra_agent.domain import ApprovalStatus


@dataclass
class AgentState:
    """
    AgentState is the single source of truth for a workflow execution run.
    """

    run_id: UUID = field(default_factory=uuid4)
    # Unique identifier for a single execution run of a workflow.
    # Every time a workflow is executed (including re-runs), a new run_id is generated.


    workflow_id: str | None = None
    # Identifier of the workflow definition being executed.
    # This refers to the logical workflow entity (not a specific run).


    workflow_version: int | None = None
    # Version number of the workflow at the time this run started.
    # Incremented when feedback modifies the workflow definition.


    step_plan_id: str | None = None
    # Identifier of the concrete step plan generated from the workflow.
    # A workflow can produce multiple step plans across versions.


    step_plan_version: int | None = None
    # Version of the step plan used for this execution.
    # Changes when the workflow is re-planned.


    current_step_id: str | None = None
    # Identifier of the step currently being executed.
    # Used to support resume, retry, and recovery logic.


    execution_history: list[dict[str, Any]] = field(default_factory=list)
    # Chronological record of executed steps.
    # Each entry should contain step_id, input, output, status, timestamps, and error info.


    snapshot_refs: list[str] = field(default_factory=list)
    # References to snapshots created before mutating steps.
    # Used for rollback and restore operations.


    approval_status: ApprovalStatus | None = None
    # Current approval state of the step plan.
    # Example values: "pending", "approved", "partially_approved", "rejected".


    last_error: str | None = None
    # The most recent error message encountered during execution.
    # Used for diagnostics and feedback generation.


    metadata: dict[str, Any] = field(default_factory=dict)
    # Additional contextual information related to the run.
    # Can store execution environment data, trace IDs, debug flags, etc.

