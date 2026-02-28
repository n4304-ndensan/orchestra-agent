from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4


@dataclass
class Workflow:
    """
    Declarative definition of an orchestration workflow.

    A Workflow describes:
    - the objective (what to achieve)
    - constraints (what must not be violated)
    - success criteria (how success is evaluated)
    - version history (how it evolves through feedback)
    """

    workflow_id: UUID = field(default_factory=uuid4)
    # Unique identifier of the logical workflow definition.

    name: str = ""
    # Human-readable name of the workflow.

    version: int = 1
    # Version number of the workflow definition.
    # Incremented whenever feedback modifies the workflow.

    objective: str = ""
    # High-level goal to achieve.
    # Example: "Build project, run tests, and generate report."

    constraints: list[str] = field(default_factory=list)
    # Hard rules that must not be violated.
    # Example: ["Do not delete existing files", "Do not push to main branch"]

    success_criteria: list[str] = field(default_factory=list)
    # Conditions that determine whether execution is successful.
    # Example: ["All tests pass", "No critical errors"]

    feedback_history: list[str] = field(default_factory=list)
    # List of feedback entries that led to workflow revisions.

    created_at: datetime = field(default_factory=datetime.utcnow)
    # Timestamp of workflow creation.

    updated_at: datetime | None = None
    # Timestamp of last update.
