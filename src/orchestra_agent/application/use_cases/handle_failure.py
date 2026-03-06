from __future__ import annotations

from orchestra_agent.executor import FailureContext, FailureHandler, RecoveryDecision


class HandleFailureUseCase:
    def __init__(self, failure_handler: FailureHandler) -> None:
        self._failure_handler = failure_handler

    def execute(self, context: FailureContext, replan_attempt: int) -> RecoveryDecision:
        return self._failure_handler.handle_failure(context=context, replan_attempt=replan_attempt)

