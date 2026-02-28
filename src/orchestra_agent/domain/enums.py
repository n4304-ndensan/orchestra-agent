from enum import StrEnum


class ApprovalStatus(StrEnum):
    """
    Represents the approval state of a StepPlan.
    """

    PENDING = "pending"
    APPROVED = "approved"
    PARTIALLY_APPROVED = "partially_approved"
    REJECTED = "rejected"
